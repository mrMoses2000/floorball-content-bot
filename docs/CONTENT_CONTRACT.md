# DB → public JSON contract

Exporter output remains version 1 and is deterministic (`sort_keys`, stable item ordering, UTC timestamps supplied by publication revision rather than wall-clock calls during serialization).

## City payload

Top level: `ok`, `version`, `generatedAt`, `cities`.

| DB source | Public field | Rule |
|---|---|---|
| `cities.slug` | `slug` | lowercase slug, max 80, unique |
| localized city fields | `nameRu/Kz/En`, `locativeRu/Kz/En`, `descRu/Kz/En`, `historyRu/Kz/En` | RU/KZ source kept in matching field; translations must be reviewer-approved; EN fallback required for new city |
| city metadata | `region`, `hero`, `geoCoords`, `dataStatus`, `updatedAt` | coordinates optional and never inferred; hero must reference approved derivative |
| aggregates | `players`, `coaches`, `clubs` | non-negative verified integers or null |
| approved clubs | `clubs_list` | max 10; contact fields only with public-contact consent |
| approved schedules | `schedule` | max 20; allowlisted weekday and validated fields |
| approved selected players | `players_list` | `active AND approved AND selected_for_publication`; max 15 |
| approved media links | `gallery` | active consent + moderation; max 30 |

Nested allowlists:

- club: `name`, `ageGroups`, `notes`, `contactName`, `contactPhone`;
- schedule: `day`, `time`, `venue`, `address`, `group`;
- gallery: `id`, `src`, `thumbnail`, `width`, `height`, `altRu/Kz/En`, `captionRu/Kz/En`, `author`, `takenAt`;
- player: `id`, `photo`, `nameRu/Kz/En`, `positionRu/Kz/En`, `bioRu/Kz/En`.

Player photo is optional. Photo is omitted/empty unless portrait consent and media moderation are approved. More than 15 eligible selected players is a validation error requiring an explicit selection; exporter never silently chooses.

Frontend-confirmed string limits: names 120, descriptions 1200, history 2400, player bio 900, captions 600, alt 300, URLs 1000. Export validation fails before the frontend sanitizer could truncate.

## Federation payload

Top level: `ok`, `version`, `generatedAt`, `federation`.

- `mission`: `statementRu/Kz`, `visionRu/Kz`, `values[]`, `goals[]`.
- `history[]` and `achievements[]`: localized `year`, `title`, `description`, plus `sourceUrl`.
- `roadmap[]`: localized `phase`, `label`, `items`, plus `done`.
- `leadership[]`: `id`, localized `name`, `role`, `bio`, `focus`, optional approved `photo`, public `email`, public `phone`.

Current frontend has RU/KZ federation fields only; English site falls back through existing static translations. This is preserved until a separate backward-compatible federation EN migration.

## Explicitly forbidden public fields

`telegram_id`, private/full phone, raw Telegram update, raw/original message, raw transcript, voice/audio paths, internal notes, consent document/path/evidence, DB creator/updater IDs, job/audit details, private email, original media path and provider metadata.

Importers store current bundles with `source=google_forms_import` and stable content hash. Dry-run/reconciliation compares canonicalized records and is repeatable without duplicates.

