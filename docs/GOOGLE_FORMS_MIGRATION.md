# Google Forms migration

Forms, Sheets, Apps Script и endpoint остаются резервным каналом в течение пилота.

1. Dry-run city/federation import и сохранить отчёт.
2. Применить `--apply`; повторный импорт того же hash не создаёт snapshot и не меняет revision.
3. Сверить `create/update/unchanged/missing_from_source`.
4. Провести полный Telegram пилот RU/KZ и публикацию в test remote.
5. Только отдельным решением перевести Forms в read-only fallback. Ничего не удалять.

