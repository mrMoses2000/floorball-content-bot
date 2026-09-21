# Threat model

## Активы

- Telegram bot token, AssemblyAI key, Agy authentication and Git deploy key.
- Телефоны, Telegram IDs, голоса, private contacts, consent evidence and internal notes.
- Approved public content, revision hashes, publication confirmations and Git history.
- Домашний компьютер, PostgreSQL and private media originals.

## Trust boundaries and threats

| Boundary/threat | Control |
|---|---|
| Unknown Telegram user | Pre-provisioned active account; verified self-contact; role cannot be self-selected. |
| Forged contact | Require `contact.user_id == message.from.id`, normalize E.164 and bind once; rebind needs superadmin approval. |
| IDOR/callback tampering | Opaque callback record + random nonce; re-check actor, role, city scope, draft status and expiry at mutation time. |
| Duplicate/replayed update | Unique `processed_updates.update_id`; idempotency keys on jobs, outbox and publication. |
| Prompt injection | User content is a data-only JSON envelope passed without a shell; Agy sandbox, isolated cwd, secret-free env, schema validation and reviewer approval. |
| Command injection | `create_subprocess_exec`/argv arrays only; no `shell=True`; paths resolved and allowlisted. |
| Malicious media/polyglot/EXIF leak | Size limits, magic-byte decode, Pillow re-encode, decompression-bomb guard, quarantine, GPS/EXIF stripping, immutable original checksum. |
| Private data in public JSON | Explicit public schemas/allowlist; consent gates; regression test rejects phones, Telegram IDs, raw paths, notes and consent documents. |
| Stolen secrets from logs/Git | Secret types, masked logs, env files mode 600, `.env` ignored, secret scan before release. |
| Queue loss/crash | PostgreSQL transaction, leased claim, attempts/dead-letter, stale-job recovery and outbox. |
| Double publish/race | Short compare-and-set state transitions, renewable owner lease, approved revision/manifest recheck, base-ref check and atomic non-force push. |
| Crash around Git push | Expected commit IDs persisted before push; durable reconciler distinguishes both-old, both-expected and mixed refs; idempotent final notification. |
| Screenshot substitution/stale approval | Persisted per-file SHA-256 and dimensions, revision + manifest binding, one-use actor callback, revalidation immediately before push. |
| Preview browser data exfiltration | Allocated loopback server, non-loopback request abort, service workers blocked, pinned Playwright Chromium and secret-free subprocess environment. |
| Compromised content editor | Least-privilege roles/city scopes, immutable revisions and audit log; reviewer + superadmin gates. |
| Home-host exposure | Long polling only; no port forwarding; optional health/admin binds localhost and future Access layer. |
| Disk exhaustion | File/publication limits, 80/90% alerts, temp cleanup and backup retention. |
| Backup theft | No plaintext secret export; restricted permissions; encrypted/off-device backup is an operator requirement. |

## Privacy defaults

Phones, Telegram identifiers, raw messages/transcripts, voice, original media paths and reviewer notes are private. Public name/bio/portrait requires an explicit current consent record; minors additionally require guardian consent. Consent legal wording remains blocked pending federation/legal approval.

## Residual risks before production

- PostgreSQL работает локально, но подтверждённого off-device backup target пока нет.
- Legal consent text and retention/deletion periods require federation/legal approval.
- Real RU/KZ AssemblyAI accuracy and billing require explicit external smoke samples.
- Для production publisher всё ещё нужна отдельная least-privilege deploy key/branch policy.
- Site lock-file на 2026-09-02 воспроизводим через `npm ci`, а `npm audit` возвращает 0; staging
  gate должен продолжать проверять high-severity regression.
