# Floorball content platform implementation roadmap

Last updated: 2026-09-01

This document is the durable execution plan for the Telegram content bot and
`/home/moses/floorball.kz`. It is intentionally stored in the bot repository because the bot
owns editorial workflow, approval state and publication orchestration.

## Goal

Build one approval-gated content platform where:

- PostgreSQL is the private source of truth for users, conversations, consent, canonical city
  data, news and audit history;
- `floorball.kz` remains a static Vite application deployed from Git/Plesk;
- city and news content is exported into deterministic, public-only JSON bundles;
- a public Telegram deep link can create a restricted new-city application without granting
  access to editorial data;
- every visually relevant publication is built and rendered in an isolated worktree;
- Telegram receives deterministic desktop/mobile screenshots of the exact revision;
- only an expiring, actor-bound callback for that revision can cause commit and atomic push;
- contact requests are durably accepted and delivered to `Knff@gmail.com`, with retry and
  operator visibility;
- Codex extracts structured data but never decides RBAC, completeness, consent, approval or
  publication and never receives production push credentials.

## Non-negotiable boundaries

1. Private phone numbers, Telegram IDs, messages, transcripts, consent evidence and original
   media never enter Git or public bundles.
2. Content changes and code/template changes use different allowlists and approval paths.
3. Routine city/news publication does not ask an LLM to edit JSX, JSON or SQL.
4. Browser screenshots in the production workflow use a pinned Playwright/Chromium process,
   not an interactive desktop Chrome session.
5. A failed test, build, screenshot, remote-ref verification or mail-delivery check must remain
   a visible failed state; it must not be reported as success.
6. Text such as "добро" is not authorization. Only a one-use callback bound to actor, revision,
   preview hash and expiry is authorization.
7. PostgreSQL migrations and integration tests use only a disposable database whose name ends
   in an approved test suffix. Fixtures may truncate their database.

## Test-driven execution rule

For each behavior:

1. Write a behavioral test against a public contract or observable boundary.
2. Run it and record the expected failure reason.
3. Implement the smallest production behavior that satisfies the contract.
4. Run the focused test.
5. Run the relevant package suite and static checks.
6. Run cross-repository contract/E2E checks when the change crosses the DB/site boundary.
7. Update the evidence log below.

Tests must not assert implementation trivia solely to make the current implementation pass.
Prefer state transitions, persisted rows, exported contracts, authorization results, rendered
UI behavior and external-boundary fakes.

## Target data and publication flow

```text
site CTA -> Telegram start payload -> application/dialogue -> immutable draft revisions
    -> reviewer decision -> deterministic canonical projector -> canonical PostgreSQL rows
    -> public allowlist exporter -> city/federation/news JSON
    -> isolated worktree -> tests/build -> Playwright screenshots
    -> Telegram media group + approve/change/cancel callbacks
    -> atomic push main + plesk-static -> manual Plesk deployment
```

## Phase P0: complete the existing editorial workflow

Release objective: an approved trainer dialogue can be deterministically and idempotently
applied to canonical city tables; reviewers can inspect, request changes or approve without
using raw SQL.

- [x] Define the approved-draft projector contract and typed dry-run result.
- [x] Reject non-approved drafts, stale revisions, changed spec/context hashes and unauthorized
      actors.
- [x] Map allowlisted trainer fields to `cities`, `city_content`, `clubs`,
      `training_schedules`, estimates and consent-aware contacts.
- [x] Treat new-city selection as an application/provisional-city path, never implicit activation.
- [x] Make application idempotent by `(draft_id, revision)` and record before/after hashes.
- [x] Add reviewer list/show/approve/request-changes Telegram callbacks.
- [x] Make a requested change create or resume a new immutable revision and invalidate old
      approval callbacks.
- [x] Add CLI dry-run/show commands for operational recovery.
- [x] Add PostgreSQL concurrency, authorization, rollback and idempotency tests.
- [x] Add health/reporting for submitted or blocked drafts.

Gate: no news or new-city publication work proceeds to production until the canonical projector
and reviewer path pass PostgreSQL E2E.

## Phase P1: real contact delivery

Release objective: public contact data displays `Knff@gmail.com`, and a submitted contact request
is durably recorded before asynchronous delivery.

- [x] Replace every public `info@floorball.kz` value and `mailto:` with `Knff@gmail.com`.
- [x] Remove the fake `preventDefault + alert(success)` implementation.
- [x] Define a versioned contact request contract: request ID, locale, name, reply-to, subject,
      message, honeypot and anti-abuse proof.
- [x] Add a dedicated loopback contact API + PostgreSQL outbox + SMTP delivery path with a fixed
      recipient, input limits, header-injection protection and plain-text mail.
- [x] Add idempotency, bounded retry, sent/dead state and Telegram operator notification.
- [x] Make frontend copy say "Заявка принята в обработку", never "Письмо доставлено".
- [x] Add frontend validation/loading/error tests and relay contract tests.
- [ ] Perform a live smoke with a unique marker and verify both durable row and Gmail inbox.

Gate: changing `mailto:` alone is not completion. The live delivery marker must be observed in
`Knff@gmail.com`.

## Phase P2: national and city news

Release objective: approved national and city-scoped news appears on the homepage, news archive,
article route and relevant city page.

- [x] Add canonical `news_items`, localized content, city links, sources and media links.
- [x] Add a versioned `news` dialogue specification and deterministic gap evaluator.
- [x] Enforce city-scope RBAC: city coaches can edit only linked cities; federation editors can
      create national news; reviewers approve; superadmins publish.
- [x] Require RU/KZ title, excerpt and safe body blocks; require media rights/alt text only when
      media is present; treat EN/gallery/video as optional.
- [x] Add `project_news_payload` and deterministic `news-content.json` generation.
- [x] Extend the publisher content allowlist without allowing JSX/config/package changes.
- [x] Replace i18n placeholder news with `NewsDataContext` data.
- [x] Add accessible non-autoplay CSS scroll-snap carousel after Geography on the homepage.
- [x] Add `/news`, `/news/:slug` and city-scoped news sections.
- [x] Add contract, localization, filtering, XSS, accessibility and route tests.

## Phase P3: public new-city application

Release objective: `/start new_city` allows an unknown person to submit only their own restricted
application; a superadmin can verify it and initialize a provisional city.

- [x] Persist Telegram start intent before contact binding.
- [x] Add applicant/application tables separate from normal users and editor roles.
- [x] Verify shared contact belongs to the Telegram sender; normalize phone; rate-limit attempts.
- [x] Restrict applicants to their own request and expose no city/editor context.
- [x] Add `city_proposal.v1` questions grouped as required-to-start, required-for-submit,
      required-for-publish and optional.
- [x] Add duplicate-name/slug detection and advisory-lock slug reservation.
- [x] Add idempotent `city-initialize --dry-run/--apply`.
- [x] Create an inactive city and city content; grant `city_coach` and exactly one city scope only
      after explicit superadmin verification.
- [x] Refactor map region metadata to be data-driven; coordinates may be absent but then the city
      must still appear in the directory.
- [x] Replace the site CTA with `https://t.me/floorball_site_agent_bot?start=new_city`.
- [x] Add applicant isolation, replay, duplicate, rate-limit and full staging E2E tests.

## Phase P4: visual preview and revision feedback

Release objective: every visual publication produces reproducible images of the exact approved
revision and supports an invalidate-and-rebuild change loop.

- [x] Add `publication_artifacts` with route, language, viewport, path, SHA-256, dimensions,
      revision hash, manifest hash and retention timestamp.
- [x] Add Telegram media-group outbox support with idempotent retry.
- [x] Run a local preview on an allocated loopback port; block non-local network requests.
- [x] Pin desktop `1280x720` and mobile `390x844`, timezone, locale and reduced motion.
- [x] Derive affected routes from the changed entity.
- [x] Fail preview if an expected screenshot is missing, corrupt, blank or has a mismatched hash.
- [x] Bind approval to actor + publication + revision hash + screenshot manifest hash + expiry.
- [x] Add Approve / Needs changes / Cancel callbacks.
- [x] Invalidate every old callback and artifact manifest when revision N+1 is created.
- [x] Add screenshot, stale-button, media-group retry and cleanup tests.

## Phase P5: crash-safe publisher

Release objective: a worker crash during Git/network activity can be reconciled without duplicate
publication or an ambiguous success message.

- [ ] Replace the long external-operation transaction with states:
      `preview_ready -> confirming -> pushing -> remote_verified -> published`.
- [ ] Use short transactions and a renewable lease around state ownership.
- [ ] Add a reconciler that compares expected commit IDs with remote `main` and `plesk-static`.
- [ ] Keep atomic push of both refs and reject base-commit drift.
- [ ] Separate content and code/template change manifests and allowlists.
- [ ] Add crash-before-push, crash-after-push, ref-mismatch and concurrent-confirm tests.

## Phase P6: staging and release

- [ ] Create a separate staging Telegram bot and PostgreSQL database.
- [ ] Use a local bare Git remote and `PUBLISH_ENABLED=false` for the first E2E.
- [ ] Test synthetic trainer flow, national news, city news and duplicate new-city application.
- [ ] Verify screenshots and stale approvals without touching production refs.
- [ ] Pilot one real national news item.
- [ ] Pilot one real city news item.
- [ ] Pilot one real new city.
- [ ] Run clean-clone install/test/lint/build, backup and restore drill.
- [ ] Manually deploy verified `plesk-static` in Plesk and smoke-test direct route reloads and
      RU/KZ/EN.

## Cross-cutting release gates

- [x] `pytest -m 'not postgres'`, full disposable-PostgreSQL suite and Ruff are green.
- [ ] Site `npm ci`, Vitest, ESLint and production build are green from a clean checkout.
- [ ] DB-to-JSON-to-frontend round trips do not expose private fields.
- [ ] RBAC/IDOR and applicant isolation tests are green.
- [ ] Media consent withdrawal removes the public derivative on the next projection.
- [ ] Contact live delivery is verified.
- [x] Both desktop/mobile screenshot sets exist for visual changes.
- [x] Old callbacks fail after revision or base commit changes.
- [ ] Remote main/static commit IDs are verified before Telegram reports success.
- [ ] Backup/restore drill succeeds.

## Execution evidence

Record focused red/green outcomes here. Do not replace raw test output; keep concise references.

### 2026-08-31 baseline

- Architecture audit: complete. Chosen path is incremental PostgreSQL -> public JSON -> Git.
- Browser audit: live `/`, `/contacts` and `/clubs` inspected through Chrome.
- Bot runtime: user bot and worker active; health returned `ok: true` on 2026-08-28.
- Authorization: `+77073645718` is bound and has active `superadmin`.
- Bot unit baseline: `54 passed, 26 deselected`.
- Site unit baseline: `24 passed`.
- Both repositories were clean after the audit.

### 2026-08-31 P0 iteration 1: consent semantics and trainer dry-run contract

- RED: `test_required_boolean_consents_must_be_affirmative_not_merely_answered` failed because
  `False` was treated as a completed consent value.
- GREEN: required boolean fields with `privacy=consent` now require exactly `True`.
- RED: trainer projection suite initially failed at collection because no deterministic
  projection module existed.
- GREEN: added a non-mutating trainer projection plan with an explicit city directory,
  existing-city resolution, new-city application blocker, required-consent blockers,
  language-specific text patches, metrics, club-private-contact redaction and schedule consent.
- Focused projection tests: `4 passed`.
- Bot non-PostgreSQL regression: `59 passed, 26 deselected`.
- Ruff: all source and test checks passed.

### 2026-08-31 P0 iteration 2: approved draft -> canonical PostgreSQL

- RED: the PostgreSQL projector suite initially failed at collection because the approved-draft
  application boundary did not exist.
- GREEN: added migration `010_canonical_projection.sql` and an atomic trainer application
  service. It verifies role, approved/current revision, content hash, mode, definition version,
  definition hash, context hash, completeness and selected-city identity before any write.
- The application updates only allowlisted city/content/estimate fields and draft-owned club and
  schedule rows. Private club contacts remain private unless their own publication consent is
  affirmative.
- RED: concurrent applications exposed a PostgreSQL `SerializationError` caused by a stale
  serializable snapshot.
- GREEN: the draft row now serializes workers under READ COMMITTED, so exactly one worker applies
  and all waiters return the same persisted application result.
- RED: the normal reviewer transition reached `approved` without pinning `approved_revision`.
- GREEN: `transition_draft` now pins the current revision on approval and clears the pin when
  changes, rejection or revocation invalidates it.
- Focused PostgreSQL projector tests: `8 passed` (approval, stale pins, authorization, rollback,
  private contact, mapping, sequential idempotency and concurrent idempotency).
- Full bot suite against `floorball_bot_test`: `93 passed`.
- Ruff, compileall and dependency integrity: passed.

### 2026-08-31 P0 iteration 4: operational inspection and health

- RED: health exposed only process/queue failures and gave no indication of submitted,
  under-review or returned editorial work.
- GREEN: `health` now includes a non-fatal `editorial_backlog` with the three state counts and
  oldest submitted timestamp; a content backlog no longer masquerades as a runtime outage.
- RED: there was no operator contract for a read-only trainer projection.
- GREEN: `project-trainer --draft ... --actor ...` returns a typed public-only plan without
  mutating canonical tables or printing private contacts. `--apply` is explicit and reuses the
  approved-revision projector and its authorization checks.
- Full bot suite against `floorball_bot_test`: `100 passed`.
- Ruff, compileall and dependency integrity: passed.
- P0 gate passed; P1/P2/P3 work may now proceed without bypassing canonical review.

### 2026-08-31 P0 iteration 3: Telegram review and correction loop

- RED: `/review` returned a placeholder without a reviewable city or callback.
- GREEN: reviewers and superadmins now receive a redacted trainer inbox, can open one exact
  revision, inspect its public summary, approve it or request changes through actor-bound,
  expiring, one-use callbacks.
- RED: `apply_projection` was accepted by the queue schema but the worker marked it dead as an
  unsupported job.
- GREEN: the worker loads the approving actor, applies the pinned revision, reports whether it
  was newly applied or already present, and enqueues a deterministic readiness scan.
- RED: `/submit` after a change request first failed because the active-session query omitted its
  context hash, then created a second draft rather than revision 2.
- GREEN: a change request reopens the original session; corrected answers append an immutable
  revision to the same draft, clear approval, invalidate all old draft callbacks and return the
  draft to `submitted`.
- Focused review/projection/workflow integration tests: `23 passed`.
- Full bot suite against `floorball_bot_test`: `97 passed`.
- Ruff, compileall and dependency integrity: passed.

### 2026-09-01 P1: durable contact acceptance and SMTP delivery

- Replaced every public `info@floorball.kz` occurrence with `Knff@gmail.com` and replaced the fake
  success alert with a localized form that reports durable acceptance only.
- Added contract v1, 16 KiB API limit, Origin allowlist, honeypot, HMAC-pseudonymized IP rate
  limit, atomic request/job persistence and fixed-recipient delivery.
- Added bounded SMTP retry, deterministic request Message-ID, sent/dead state and a terminal
  Telegram notification to bound superadmins.
- A lost HTTP response now reuses the same request UUID for unchanged form data. A worker crash
  leaving a request in `sending` can be reclaimed from the durable job instead of being killed.
- Added a hardened loopback `floorball-contact-api.service` and documented the Plesk reverse proxy,
  Gmail app password, readiness check and live-smoke procedure.
- Full bot suite against `floorball_bot_test`: `110 passed`; Ruff, compileall and dependency
  integrity passed.
- Full site suite: `30 passed`; ESLint and production Vite build passed.
- Release gate remains open until operator-owned SMTP/Plesk secrets are configured and a unique
  marker is observed both in `contact_requests` and the `Knff@gmail.com` inbox.

### 2026-09-01 P2: approval-gated national and city news

- Added a versioned `news` Telegram dialogue, canonical `news_items`, immutable approved-revision
  projection, city-scope RBAC and deterministic public-only export.
- Added the news bundle to the isolated publisher allowlist and its parser/build verification;
  publication still cannot change JSX, configuration or packages.
- Replaced placeholder homepage news with approved data, added an accessible non-autoplay
  scroll-snap carousel after Geography, archive and article routes, and city-scoped sections.
- RU/KZ content is mandatory, EN is optional with client fallback, and typed body blocks render as
  text without HTML execution. Media is optional but requires rights and RU/KZ alt text when used.
- Full bot suite against `floorball_bot_test`: `115 passed`; Ruff, compileall and dependency
  integrity passed.
- Full site suite: `36 passed`; ESLint, the public news contract and production Vite build passed.

### 2026-09-01 P3: isolated new-city applications

- The `new_city` deep link now persists an expiring start intent before contact binding. The
  applicant must share their own Telegram contact; contact and answer attempts are bounded.
- Applicants and applications remain outside `users`, roles, city scopes, conversations and agent
  context. A submitted applicant can read or change only their own application.
- Added the versioned `city_proposal.v1` questionnaire, immutable event trail, duplicate detection,
  explicit superadmin review callbacks and advisory-locked slug reservation.
- `city-initialize` is dry-run by default. `--apply` works only for an explicitly verified request,
  creates an inactive city and content, then creates one `city_coach` with exactly one city scope.
- Map region aliases now come from city data; a city without coordinates still remains in the
  directory. The public CTA opens `https://t.me/floorball_site_agent_bot?start=new_city`.
- Full bot suite against `floorball_bot_test`: `122 passed`; Ruff, compileall and dependency
  integrity passed. Full site suite: `37 passed`; ESLint and production Vite build passed.

### 2026-09-01 P4: deterministic visual preview and revision feedback

- Added migration `015_publication_artifacts.sql`: every screenshot persists its route, language,
  fixed viewport, path, dimensions, SHA-256, exact revision hash, manifest hash and retention time.
- The publisher derives affected city/news/federation routes, starts Vite on an allocated
  `127.0.0.1` port and captures RU/KZ/EN at `1280x720` and `390x844` with fixed timezone,
  locale, reduced motion and wall-clock behavior. Browser routing rejects non-loopback network.
- Preview validation rejects an incomplete matrix, unsafe/missing/corrupt/blank images, wrong
  dimensions and changed image or manifest hashes. Expired cleanup refuses paths outside the
  dedicated artifact root.
- Telegram delivers screenshot batches through a durable retrying media-group outbox. Approve,
  Needs changes and Cancel are actor-bound, one-use and tied to publication, revision, manifest
  and expiry; revision N+1 invalidates the old publication, artifacts and callbacks.
- Real browser smoke: Playwright Chromium `145.0.7632.6` (revision `1208`) produced six homepage
  screenshots; all passed the production validator.
- Focused P4 suite against `floorball_bot_test`: `21 passed`. Full bot suite: `131 passed`; Ruff,
  compileall and dependency integrity passed. Clean `npm ci` site suite: `37 passed`; ESLint and
  production Vite build passed. The existing npm audit still reports 14 dependency advisories
  and remains separate frontend-maintenance work.
