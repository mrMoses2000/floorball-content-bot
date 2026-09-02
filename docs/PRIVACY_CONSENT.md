# Privacy и consent configuration

Internal по умолчанию: телефоны, Telegram IDs, raw messages/transcripts/voice, private media paths, reviewer notes и consent evidence.

Публикация имени/биографии и портрета — разные scopes consent. Для несовершеннолетнего `guardian_confirmed` обязателен. Withdrawn consent блокирует следующий export и требует approved removal revision; уже опубликованная Git history не переписывается.

Текст юридического согласия, retention срок голосов/исходников и процедура data subject request должны быть утверждены федерацией или юристом. Технический placeholder `pending-legal-approval` нельзя показывать пользователю как готовую юридическую политику.

Public exporter сериализует только allowlist Pydantic models. Тесты запрещают phone/Telegram/raw voice/internal notes/consent evidence в JSON.

Перед очередной readiness-проекцией worker сверяет последнее действующее согласие
`media_publication`. Статус `withdrawn` удаляет только производную копию внутри
`MEDIA_ROOT/derived`, снимает `selected_for_publication`, очищает `derivative_path` и оставляет
audit event. Оригинал не удаляется автоматически. Путь вне управляемого корня блокирует
проекцию; вручную проверить/повторить reconciliation можно командой
`.venv/bin/floorball-bot media-consent-reconcile`.
