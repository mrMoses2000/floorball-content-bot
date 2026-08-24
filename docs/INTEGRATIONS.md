# Настройка интеграций

## Telegram

1. Создайте отдельного бота через BotFather и отключите privacy mode только если это действительно нужно для групп.
2. Поместите токен в защищённый `TG_API_KEY`.
3. Запустите `floorball-bot bot`. Процесс вызывает `getWebhookInfo`; webhook удаляется только у этого токена и с `drop_pending_updates=false`.
4. Пользователь должен быть заранее создан CLI-командой. `/start` выдаёт кнопку `request_contact`; текстовый телефон не авторизует.

Long polling не требует домена, HTTPS, Cloudflare Tunnel, port forwarding или публичного входящего порта.

## AssemblyAI

- Пользовательское имя секрета: `ASSEMBLI_AI`; также принят canonical alias `ASSEMBLYAI_API_KEY`.
- RU и другие проверенные batch-языки идут через pre-recorded API.
- KZ/KK маршрутизируется в Whisper Streaming (`whisper-rt`) после ffmpeg 16 kHz mono conversion и отправляется с wall-clock pacing. Session всегда получает `Terminate`.
- Обычные тесты используют `FakeTranscriber` и не расходуют credits.
- Для external smoke подготовьте короткие аудио с юридически допустимым содержимым и запустите отдельный тестовый сценарий; production-голоса не используйте как тестовые fixtures.

## Codex CLI

Проверьте `codex --version` и авторизацию для отдельного Linux user. Wrapper запускает только `codex exec --ephemeral --sandbox read-only`, передаёт prompt по stdin, включает JSON Schema, ограничивает окружение/вывод/timeouts и не наследует Telegram/AssemblyAI secrets.

Официальная документация: https://learn.chatgpt.com/docs/non-interactive-mode

## GitHub deploy key

1. Создайте отдельную ED25519 key pair от имени `floorballbot` без использования личного PAT.
2. Добавьте public key как write-enabled deploy key только в `Sherzattv/floorball.kz`.
3. Создайте SSH alias `github-floorball` с `IdentitiesOnly yes` и pin GitHub host key после ручной проверки fingerprint.
4. Измените repository remote на `git@github-floorball:Sherzattv/floorball.kz.git` только после тестового fetch/push в отдельную ветку.

Никогда не добавляйте private key в репозиторий или `/etc/floorball-bot.env`.

