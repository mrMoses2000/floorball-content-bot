# Production release: bot is the content source

## Goal and current boundary

Telegram polling and authenticated Mini App collect private drafts; PostgreSQL
owns sessions, roles, consent, reviewed canonical content and durable jobs.
Worker owns transcription, AI extraction, image processing and publication.
GitPublisher verifies contracts, builds previews and atomically pushes `main`
and `plesk-static`. Plesk serves the static site from `httpdocs`.

Current reviewed text projections work. Trainer players/media and leadership
portraits are incomplete; official PDF registry is private only. Website still
contains Google Forms redirects, Apps Script integrations and a contact form
whose configured backend is absent. Plesk static repository is stale.

## Required behavior

1. Both existing Telegram IDs retain superadmin and are active; no new roles or
   users are implicitly granted privilege. Record activation in the audit log.
2. Bot/Mini App are the source of new content. Remove active Sheets/Forms
   network paths and obsolete deployment instructions. Applied migrations and
   existing imported records remain readable; do not rewrite migration history.
3. Uploaded images are tied to a precise session field, not an AI-guessed URL.
   Private original + stripped WebP derivative; creator/session checks, bounds,
   role checks, stale-write checks and explicit publication rights precede use.
4. Reviewed trainer data creates stable player records and city media. Consent
   withdrawal must exclude names/photos/assets at export, including retries.
   Leadership portraits use the same managed media boundary.
5. Verified, explicitly public PDFs reach a public documents bundle and site.
   Unverified, expired, withdrawn or private documents remain private.
6. Existing valid previews/approvals cannot silently authorize different bytes.
   All new public asset roots are allowlisted, path checked and hashed.
7. Existing domain can provide a short bot/Mini App entry path. Shared hosting
   settings determine whether this is a redirect or a reverse proxy. Do not
   claim a redirect hosts the Mini App on the website domain.
8. Site release uses the reviewed branch, native checks, real screenshots and
   Plesk fetch/deploy with public hash and route verification. Restart behavior
   and health are checked on installed bot services.

## Compatibility and migration

Removing active Google Forms collection is explicitly requested. Retain
historical database migrations and offline import compatibility. Before adding
media/session associations or public document fields, use a new additive
numbered migration; never edit the applied 001–022 files. Preserve schema pins
on existing sessions unless a versioned replacement and transition are needed.

## Implementation order and affected components

- Access activation: exact production user row, audit_log, protected rollback
  metadata; verify actor resolution for both IDs.
- Complete review: auth, polling, queue leases/outbox, dialogue memory,
  projection, consent, media paths, publisher, Mini App and site data adapters.
- Content integration: additive migration + shared media attachment module;
  Telegram/worker/Mini App upload entry points; trainer/federation projections;
  exporters/domain contract/publisher; relevant UI and PostgreSQL regressions.
- Retire legacy collection: website form components, Forms redirects, Apps
  Script templates and Google-dependent contract tests; replace those tests
  with native public JSON fixtures. Keep native sync scripts used by publisher.
- Release: native site pre/post build, contract tests, bot Ruff/tests, Mini App
  lint/tests/build; protected backup, install, restart, signed bootstrap for both
  users; atomic site refs, Chrome Plesk deployment and public verification.

## Failure and evidence rules

Retries must not duplicate profiles, consent or attachment links. Late upload
must not mutate a completed/submitted session. Untrusted file text cannot read
arbitrary files or fetch arbitrary remote URLs. Hash the materialized bytes,
remove stale managed assets only inside explicit roots, and preserve imported
assets. Capture exact commands/results and deployment versions in a review
summary. Missing credentials or interactive GitHub authentication are explicit
release gaps, not successful checks. Do not use real private content as test
fixtures or send pilot messages to users without separate authorization.

## Release completion hook

The bot invokes the existing Plesk generated webhooks only after atomic Git refs
are remotely verified. Credentials live in private .env, allowed host/HTTPS are
validated. A publication is finalized only after the public index hash equals
the static commit. Network/hosting failure remains remote_verified and is retried
by existing reconciliation; no second independent approval is required.
