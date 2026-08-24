# Архитектура Floorball Content Bot

## Граница системы

Система принимает Telegram updates, авторизует заранее заведённых пользователей, сохраняет входные данные и медиа, формирует версионированные черновики, проводит user/reviewer approval и только затем создаёт детерминированный preview публикации для `floorball.kz`.

Внешние зависимости: Telegram Bot API, AssemblyAI, локальный Codex CLI, PostgreSQL, ffmpeg/ImageMagick/Pillow, локальный clone `floorball.kz` и GitHub. Plesk остаётся ручным последним шагом.

## Deployment units

```mermaid
flowchart LR
  TG[Telegram API] -->|long polling| BOT[floorball-bot]
  BOT -->|transaction| PG[(PostgreSQL)]
  PG --> WORKER[floorball-worker]
  WORKER --> AAI[AssemblyAI]
  WORKER --> CODEX[Codex CLI read-only]
  WORKER --> MEDIA[Local private media]
  PG --> PUB[floorball-publisher]
  PUB --> WT[Isolated git worktree]
  WT --> TESTS[Parser tests + Vitest + Vite build]
  TESTS -->|explicit confirmation| GH[GitHub main + plesk-static]
  GH -->|manual| PLESK[Plesk deploy]
```

- `floorball-bot`: `getUpdates`, durable acceptance, authorization handlers, Telegram replies/outbox dispatch, graceful SIGTERM.
- `floorball-worker`: claims jobs with `FOR UPDATE SKIP LOCKED`, transcribes, extracts structured data, prepares media derivatives and resumes retries.
- `floorball-publisher`: advisory lock, approved hash check, isolated worktree, deterministic export, tests/build, preview; commit/push is a second explicit action.
- `floorball-backup`: `pg_dump`, media/config manifest and retention; secrets are not copied into reports.

## Persistence and queue

Numbered idempotent SQL migrations manage normalized tables. Jobs have `pending/running/retry/dead/succeeded`, attempts, `available_at`, `locked_at`, `locked_by`, idempotency key and last error. A short transaction atomically claims rows using `FOR UPDATE SKIP LOCKED`. Stale `running` rows return to retry after lease expiry.

Telegram acceptance inserts `processed_updates`, normalized message and any required job/outbox rows in one transaction. `update_id` uniqueness prevents duplicate entities. Outbox delivery is at-least-once with an idempotency key and persisted Telegram message ID; the system promises effectively-once business mutation, not exactly-once networking.

## Request/event flow

1. Poll update with current durable offset.
2. Validate type/size; open DB transaction and insert unique `processed_updates` row.
3. Re-check contact ownership, active user, roles and city scopes for every mutation/callback.
4. Persist message/media metadata; enqueue work and/or response in the same transaction.
5. Advance polling offset only after commit.
6. Worker claims job, calls bounded external provider, stores result/revision and enqueues reply.
7. User confirms transcript and draft. Reviewer separately approves. State machine rejects invalid transitions.
8. Superadmin builds publication preview. A second confirmation matching preview nonce, revision hash and base commit permits push.

## Interfaces

- Configuration: environment only; `.env` is accepted for local development. Canonical secret names are `TG_API_KEY` and the user-provided `ASSEMBLI_AI`; alias `ASSEMBLYAI_API_KEY` is supported.
- Telegram callbacks contain opaque server-side action ID plus nonce, never role/city authority.
- Extractor input is untrusted text inside a delimited JSON envelope. Output must satisfy a Pydantic-generated JSON Schema; one validation repair is permitted.
- Public export uses explicit Pydantic allowlist models, never DB-row serialization.

## Codex subprocess boundary

The wrapper invokes `codex exec --ephemeral --sandbox read-only --ignore-user-config --output-schema ... --output-last-message ...` with an argv list, a secret-free allowlisted environment, isolated cwd, one-process semaphore, timeout and stdout/stderr caps. Process group receives TERM then KILL. User text never becomes a shell command.

The official OpenAI non-interactive-mode documentation confirms that `codex exec` is intended for scripts, supports `--ephemeral`, and defaults to read-only; local `codex exec --help` confirms the exact flags installed here.

## Failure handling and observability

- Retry only timeout, connection, 429 and provider 5xx errors using bounded exponential backoff plus jitter.
- Auth, schema, consent, invalid transition and permanent Telegram errors go directly to review/dead state.
- JSON structured logs include correlation/update/job/publication IDs, never full phone/token/transcript/private paths.
- Health command checks DB, polling heartbeat, disk, dead jobs, latest backup and publication.
- No inbound production port is required. Optional health endpoint binds only `127.0.0.1`.

## Resource model

One Codex subprocess globally, one transcription job by default, small asyncpg pool, no Redis/Kafka/Kubernetes/local LLM. Originals remain outside Git; bounded public derivatives are copied only into an isolated publication worktree.

