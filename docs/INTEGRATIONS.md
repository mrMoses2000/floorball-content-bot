# Настройка интеграций

## Telegram

1. Создайте отдельного бота через BotFather и отключите privacy mode только если это действительно нужно для групп.
2. Поместите токен в защищённый `TG_API_KEY`.
3. Запустите `floorball-bot bot`. Процесс вызывает `getWebhookInfo`; webhook удаляется только у этого токена и с `drop_pending_updates=false`.
4. Пользователь должен быть заранее создан CLI-командой. `/start` выдаёт кнопку `request_contact`; текстовый телефон не авторизует.

Long polling не требует домена, HTTPS, Cloudflare Tunnel, port forwarding или публичного входящего порта.

## Форма связи и SMTP

1. Сгенерируйте отдельный случайный `CONTACT_API_SECRET` длиной не менее 32 байт. Он используется
   только для HMAC-псевдонимизации IP и не передаётся браузеру.
2. Укажите `SMTP_USERNAME` и отдельный app password в `SMTP_PASSWORD`; обычный пароль Gmail не
   используйте. Получатель жёстко задан как `Knff@gmail.com` и не принимается из HTTP-запроса.
3. Запустите `floorball-bot contact-api` на `127.0.0.1:8088` и оставьте worker запущенным.
4. На домашнем сервере создайте постоянный HTTPS-маршрут, например:
   `tailscale funnel --bg --yes --set-path=/floorball-contact http://127.0.0.1:8088`.
   В сборке сайта задайте `VITE_CONTACT_API_URL=https://<stable-host>/floorball-contact/api/contact/v1/requests`.
   Plesk при этом отдаёт только статику и не должен соединяться с домашним IP напрямую.
5. Проверьте `GET http://127.0.0.1:8088/healthz` и публичный HTTPS health URL, затем отправьте одну заявку с уникальным marker,
   найдите её UUID в `contact_requests` и подтвердите письмо в `Knff@gmail.com`.

API возвращает только факт долговременного принятия (`202 accepted`), а не обещание доставки.
Повтор неизменённой формы использует тот же UUID. Временная SMTP-ошибка повторяется не более пяти
раз; окончательная ошибка остаётся видимой в БД и отправляется superadmin через Telegram outbox.
Не включайте публичный маршрут до настройки SMTP: иначе заявка сохранится, но delivery job
завершится как `mailer_not_configured`.

## AssemblyAI

- Пользовательское имя секрета: `ASSEMBLI_AI`; также принят canonical alias `ASSEMBLYAI_API_KEY`.
- RU и другие проверенные batch-языки идут через pre-recorded API.
- KZ/KK маршрутизируется в Whisper Streaming (`whisper-rt`) после ffmpeg 16 kHz mono conversion и отправляется с wall-clock pacing. Session всегда получает `Terminate`.
- Обычные тесты используют `FakeTranscriber` и не расходуют credits.
- Для external smoke подготовьте короткие аудио с юридически допустимым содержимым и запустите отдельный тестовый сценарий; production-голоса не используйте как тестовые fixtures.

## Agy CLI

Проверьте `agy --version` и авторизацию для того же Linux user, от которого работает worker.
Production wrapper использует модель `gemini-3.8-flash` с effort `high`, print mode,
`--sandbox`, отключённые slash-команды и строгую JSON Schema. Процесс запускается прямым argv
без shell; Telegram/AssemblyAI/SMTP secrets в дочернее окружение не наследуются. Значения можно
переопределить через `AGY_CLI`, `AGY_MODEL`, `AGY_EFFORT` и `AGY_TIMEOUT_SECONDS`.

## GitHub deploy key

1. Создайте отдельную ED25519 key pair от имени `floorballbot` без использования личного PAT.
2. Добавьте public key как write-enabled deploy key только в `Sherzattv/floorball.kz`.
3. Создайте SSH alias `github-floorball` с `IdentitiesOnly yes` и pin GitHub host key после ручной проверки fingerprint.
4. Измените repository remote на `git@github-floorball:Sherzattv/floorball.kz.git` только после тестового fetch/push в отдельную ветку.

Никогда не добавляйте private key в репозиторий или `/etc/floorball-bot.env`.
